# Opening prompt (paste this to start the first Claude Code session)

---

We're building a live translator for videos with hardcoded (burned-in) subtitles, starting with Chinese-language TV dramas. Read `CLAUDE.md` and `DOCUMENTATION.md` in full before doing anything; they contain the architecture, the extension<->service contract, and the invariants. Do not violate the invariants.

Current state: **nothing is built yet.** This is a greenfield repo.

We are building **Shape B** first: a Firefox extension (Manifest V2, plain JS) that captures video, plus a local Python sidecar that does the OCR and translation. The two talk over the HTTP contract defined in `CLAUDE.md`. The contract is sacred: the extension must never assume anything about how the service is implemented, so we can later swap the Python sidecar for in-browser wasm without touching extension code.

**Do not try to build the whole thing at once.** We're going stage by stage, proving each piece before moving on. Here is the ONLY task for this first session:

## First deliverable: the capture + change-detection skeleton (no OCR, no LLM yet)

Build the extension scaffold and get change detection working *visibly*, with no OCR and no translation wired up.

1. Extension skeleton: `manifest.json` (MV2), a content script, and whatever minimal UI lets me point it at a video and draw/define the crop box over the player.
2. Capture loop: fast path grabs the `<video>` element to a canvas; if the crop comes back uniformly near-black, fall back to `getDisplayMedia`. Sample the crop at ~10fps.
3. Text-mask + change detection: threshold the crop to near-white + dark-stroke text, downscale to a coarse silhouette, hash it, and detect when the hash changes (new line) vs holds stable (same line). Detection must run on the mask, not raw pixels, so background motion doesn't trip it.
4. Empty-mask idle state: count surviving text pixels after masking. Below a floor = silence: emit "idle" and do nothing. Track the two edges — empty->populated ("LINE START") and populated->empty ("LINE END"), the latter with ~2-sample hysteresis. (No overlay exists yet to clear, but wire the LINE END event now; session 3 hooks overlay-clear to it.)
5. Basic not-text rejection: don't fire "NEW LINE" for a full-box white fill. At minimum reject masks that are one large contiguous blob rather than a horizontal row of thin character-sized clusters. (Full 3b/3c fingerprinting — pixel-pinning, orientation, prop rejection — comes later; for session one just don't let an explosion register as a line.)
6. **Debug view:** render the computed text-mask to a small visible canvas next to the video, and log LINE START / stable / LINE END / idle / rejected-not-text to the console as the subtitle changes. This is the whole point of session one, I want to *watch* the mask stay clean and stable while the video behind moves, snap only when the actual subtitle changes, go empty in silent scenes, and NOT light up on a white flash.

Read DOCUMENTATION.md sections 3, 3a, 3b, 3c for the full detector model before writing the detector.

Stop there. No OCR, no translation, no overlay of results. Once the mask looks clean and change detection fires correctly on a real cdrama clip, we've de-risked the hardest part and we'll wire the sidecar contract next.

Set up the repo structure sensibly (separate `extension/` and `sidecar/` directories even though the sidecar is empty for now), and give me clear instructions to load the unpacked extension in Firefox and test it.

---

## Notes for later sessions (do NOT do these yet, just so you know the arc)

- Session 2: Python sidecar with the `/translate` endpoint matching the contract, wired to PaddleOCR / RapidVideOCR-style OCR + majority vote + dedup. Test with a mocked translation first.
- Session 3: wire the extension to POST the 3 spaced frames to the sidecar; render the returned translation as an overlay pinned to the player bbox.
- Session 4: real translation provider (Groq) behind a swappable interface; drop-don't-queue enforcement via frame_id.
- Session 5: tuning (thresholds, sampling, lag), bilingual toggle, then AMO packaging.
- Much later (Shape A): replace the Python sidecar with in-browser wasm OCR, same contract.
