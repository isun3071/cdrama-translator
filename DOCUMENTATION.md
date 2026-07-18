# Design Documentation

The *why* behind the build. `CLAUDE.md` is the lean operating manual; this is the full reasoning so decisions can be understood and revisited rather than cargo-culted.

## Problem and origin

Chinese-language TV dramas reliably burn Mandarin subtitles directly into the video image. This is a production norm: even domestic viewers need the hanzi because spoken dialogue (fast speech, topolects) does not always map cleanly to the written form, and foreign characters frequently speak other languages (Russian, Japanese, English) while the subtitle stays Mandarin.

That last point is the sharp origin of this tool. Audio-based approaches (speech recognition -> translate) break on multilingual, code-switching audio. The burned-in subtitle is the one channel already normalized to a single written language for every speaker. OCR-ing that channel is therefore the *most* robust method precisely where alternatives are weakest.

The unlock: most cdramas are never subtitled into languages beyond English (and often not even that promptly). Speakers of Portuguese, Indonesian, Arabic, Hindi, etc. are largely shut out. Translating the burned-in text into any target language gives that audience functional access to content they currently cannot watch at all.

## What already exists (and the gap)

- **Real-time overlay tools** (e.g. Immersive Translate) read the platform's *caption track*. They explicitly cannot handle hardcoded/burned-in subtitles, and they require per-platform integration.
- **Offline pixel-OCR tools** (VideOCR, videocr-PaddleOCR, RapidVideOCR + VideoSubFinder, SubHero, SubExtractor) do OCR burned-in subtitles well, but they are file-in / SRT-out, batch, offline.
- **Live pixel OCR** exists on other platforms (e.g. an iOS screen-scanning app) but not as a browser extension on an arbitrary web player.

**The gap this fills:** live + hardcoded-pixel OCR + translate-and-overlay + runs on any web video player. Nobody has combined these. The vision components are mature and open-source; the novel part is the real-time browser harness around them.

## Legal / scope reasoning

OCR-ing pixels off a DRM-protected paid platform invites circumvention arguments (DMCA 1201) and, if you inject JS to pull pre-decryption caption data, CFAA-type arguments. OCR-ing pixels that are already rendered in the clear, for private personal viewing, with no redistribution, is a far weaker target and is close to the "hover to translate" behavior mainstream tools ship openly. Not legal advice, but the safe line is: **in-the-clear video, personal use, no redistribution, don't target DRM platforms.** Conveniently, the direct `<video>` canvas grab already returns black on DRM, so the technical and the comfort boundary coincide.

## Pipeline, stage by stage

### 1. Capture
Two doors. Fast path: grab the `<video>` element to a canvas and `getImageData`. Works on in-the-clear video (e.g. YouTube). Fallback: if the canvas comes back uniformly near-black (DRM or protected compositing), use `getDisplayMedia` to capture the shared tab's composited pixels. Same downstream either way; only the source swaps.

Screen-capture also delivers the **player-agnostic** property: reading composited pixels means the tool never needs to know which player is underneath. Infinite platform coverage, zero per-platform code. This is the primary moat.

### 2. Crop
A box anchored to the **player element's bounding box**, expressed as fractions of player height/width, not absolute screen coordinates. This survives resize and fullscreen. Subtitle position is rock-stable within a given source, so the box can be set once (or auto-calibrated on the first few lines) and then treated as fixed, which removes the need for per-frame text *detection* and lets us just watch a known region.

### 3. Change detection (on a text-mask, never raw pixels)
The central problem: borderless subtitles sit directly on moving video (no subtitle background box), so raw-pixel diffing trips constantly on background motion even when the text is unchanged.

Solution: isolate the text layer *before* looking for change.
- Threshold to near-white, low-saturation pixels (subtitle fill). Mid-tone background (suits, faces, blackboards, crowds) falls out.
- Require a dark-stroke border around the white pixels. This kills surviving white blobs (bright shirts, reflections) that lack the text's characteristic dark outline.
- Downscale the resulting mask to a coarse silhouette and hash it. Compare hashes across frames.

Because the background was subtracted before hashing, the mask changes only when the *text* changes. Background motion is invisible to the detector. cdrama subtitles snap (0->100% opacity instantly, no fade), so a real change is a clean single-frame hash discontinuity; there is no transition to wait out.

Worst case: white text over a genuinely white background (snow, bright wall). The dark-stroke requirement rescues most of these; stroke-less white-on-white is the genuine failure case and is hard for human eyes too.

#### 3a. The empty-mask idle state (silence detection)
The box being empty is not an edge case; it is half the state machine. When no character is speaking there is no subtitle, so after masking, **count the surviving text pixels.** Below a floor (few white-stroke pixels), the scene is silent: do nothing. No hash, no OCR, no vote, no request. The box just watches black.

This gives two transitions the detector otherwise wouldn't have, because change detection only ever caught subtitle *changes*, never subtitle *ends*:
- **empty -> populated** = a line started (begin processing).
- **populated -> empty** = the line ended. This must **clear the overlay**, so a stale translation doesn't linger over a silent reaction shot. The empty-mask state does double duty: skip processing *and* take any showing translation down.

Add hysteresis: require the mask empty for ~2 consecutive samples before clearing, so a one-frame dip between two back-to-back lines doesn't flash the overlay off and on.

#### 3b. Rejecting not-text (the explosion problem)
A full-viewport white flash (explosion, cut to white) passes the near-white threshold everywhere, so whiteness alone cannot identify subtitles. The fix is not a harder white threshold; it is to **stop treating whiteness as the identifier and lean on structure and stability**, which are already being computed. Subtitle white is *thin, stroked, character-arranged, and stable*; a white blob is none of these.

Discriminators (an imposter must pass all; a full-frame white passes none but the first):
- near-white (passes) — the cheap pre-filter only.
- thin strokes bordered by dark — a solid white region has no internal dark-stroke structure; only its outer edge borders dark. Fails.
- character-row spatial layout — subtitles are a horizontal row of discrete character-sized clusters with gaps; a flash is a uniform fill. Fails.
- stable across 2-3 samples — a subtitle snaps in and holds 2-4s; an explosion churns/dissipates every frame and never settles. Fails.

The filters built for other reasons (dark-stroke for bright shirts, stability for transitions, character-layout for OCR) incidentally reject full-frame whites. No special explosion handling is needed. The one genuinely hard sub-case is a **subtitle displayed during** a white flash, which collapses to white-on-white: rescue by keying on the dark **stroke** instead of the white **fill** when the box is mostly bright (detect "box is bright" -> switch mask from find-white-fill to find-dark-stroke-edges-arranged-as-text). Same stroke information, keyed on the dark part.

#### 3c. Rejecting not-the-subtitle (prop text: held paper, chalkboard)
Sections 3b's discriminators answer "is this text?" Prop text (a held paper, background chalkboard) is *real* text and passes them. These require the harder question "is this **the subtitle**?", answered by the properties of being a **rendered overlay** rather than a **filmed object**. An overlay is flat, fixed, pixel-perfect-still, and synthetically stroked; the physical world is none of those.

| discriminator | subtitle | held paper | chalkboard |
|---|---|---|---|
| inside a tight position band | yes | usually no | usually no |
| pixel-pinned, no frame-to-frame drift | yes | no (hand jitter) | no (camera drift) |
| perfectly horizontal, frontal, undistorted | yes | no (tilted/perspective) | often no |
| synthetic white-fill + dark-stroke font | yes | no (dark ink on light) | no (soft chalk) |

The two heavy lifters:
- **Tight crop.** The box should hug the actual subtitle line (~8-10% tall), not a loose bottom-15%. Most props are then simply outside the box and never become candidates. This is the single biggest defense and it is free.
- **Pixel-pinned stability.** Beyond "same text present," check the text cluster's bounding box is at the *exact same pixel coordinates* across the 3 spaced frames. A burned-in overlay is bolted to fixed coordinates; nothing physical holds still to the pixel — a held paper drifts with the hand, a board drifts with the camera. That positional micro-jitter is a signature real subtitles never have.

Orientation (subtitle text is rigidly 0-degree horizontal and frontal; detected text with any rotation/perspective is in-world) and the font/stroke signature (synthetic white-core-dark-halo vs. dark ink or soft chalk) are bonus insurance.

Residual case to accept, not over-engineer: a prop deliberately framed flat, steady, and frontal *inside* the subtitle band (e.g. a character holding a sign at camera in lower-center). This can fool position + orientation. Temporal cadence (subtitles change with speech rhythm; a held sign persists unnaturally long or appears/disappears with prop motion) is the last discriminator, but if a director frames a sign exactly where subtitles go, translating it is a mild and arguably helpful failure — it is readable text the viewer was meant to read — not a garbage-over-silence failure. Do not chase the last 1%.

**Principle for all of 3a-3c:** whiteness finds candidates; *text-ness* (structure + stability) confirms it is text; *overlay-ness* (position + pixel-pinning + flatness + synthetic stroke) confirms it is the subtitle. Every new false-positive source is fixed by leaning on a structural/temporal/overlay discriminator that whiteness-confusion cannot fake, never by tuning the white threshold harder.

### 4. Sampling and the OCR vote
Sample the box at ~10fps (subtitle is on screen 2-4s; 10fps gives ample samples without tripling OCR load). On a detected change, grab **3 spaced frames** at t, t+100ms, t+200ms. Spacing matters: back-to-back frames share the same momentary background noise and can share the same misread, defeating the vote. Spaced frames see *different* background noise, so the text is the only signal consistent across all three.

OCR all 3 concurrently, then **per-character majority vote** (align strings, take the most common character per position). This corrects single-frame OCR errors caused by busy backgrounds. "Consistent enough" means per-character 2-of-3 agreement, not exact string match. If all three disagree wildly, treat as no-clean-text and wait.

The masked image (background subtracted) is also what gets fed to OCR, not the raw crop, improving accuracy on borderless-on-video lines. Same preprocessing step serves both change detection and OCR quality.

### 5. Dedup
After the vote produces a clean line, compare it (string similarity, ~80 threshold) against the last shipped line. If effectively identical, do not re-translate; the same line is simply still on screen. This prevents firing many identical translation calls for one 3-second subtitle. Lives service-side because it needs the OCR text.

Edge case to consider later: a line of dialogue that legitimately repeats as two separate subtitle events (e.g. a word said twice). Pure text-dedup would merge them. Low priority for v1.

### 6. Translation
The output is tiny (~20 tokens), so latency is dominated by **time-to-first-token, not throughput.** Model choice is therefore TTFT-driven:
- **Groq**: sub-100ms TTFT on a warm endpoint; the pick for lowest latency. Real-time-oriented.
- **Cerebras**: comparable TTFT, huge throughput (overkill here but fine).
- **DeepSeek V4 Flash**: extremely cheap ($0.14/$0.28 per M, cache-hit input ~$0.0028/M) but only ~55-60 tok/s and higher TTFT; good for cost, wrong for latency.
- **Local (Ollama, small Qwen)**: zero cost, no key, at some latency/quality cost. The fully-free fallback.

Cost is a non-issue regardless: input is trivial and output ~20 tokens, landing around a cent per episode on hosted APIs, or free locally. Keep the provider behind a small swappable interface.

Batching + a stable system-prompt prefix (for prefix caching) further cut cost, but cost was never the constraint; latency is.

#### 6a. Context and the split-sentence problem (translation accuracy)
Reactive translation uses only backward context (lines already seen). But a single sentence is often split across 2-3 sequential subtitle events, and the character may pause between them for suspense or emphasis. Line N's correct translation can depend on line N+1, which hasn't appeared yet. This is not a fixable bug; it is the **linearity constraint** of all real-time translation, the same problem human simultaneous interpreters face (they cope via the "salami technique" / chunking).

Two cases:
- **Case 1 — grammatically completable split** (我昨天 / 去了图书馆): line N is predictably incomplete; translate as an open fragment ("Yesterday I...") and N+1 completes it. The viewer stitches it. Common, low-risk.
- **Case 2 — meaning-changing split** (我爱你 / 才怪 → "I love you" flipped to sarcasm by the tail): translating N alone commits to a reading the completion may reverse. Cannot be solved reactively — the needed information does not exist yet at translation time.

This is a well-studied field (Simultaneous Neural MT). The proven approach, and the one to use:
1. **Translate on arrival, then re-translate + revise ("re-translation" / "screen-rewriting").** Show line N's provisional translation immediately (responsiveness preserved). When N+1 arrives and looks like a completion (short gap, no terminal punctuation on N, grammatical continuation), re-translate N+N+1 together and update the overlay. Research finding to lean on: **users prefer responsiveness over stability** — do NOT buffer-and-wait for complete sentences; show fast and revise.
2. **Context-aware decoding.** Translate each line with the sliding window of recent **source** lines as reference (see 6b), so chunks read coherently even without revision.
3. **Tail-masking to minimize flicker.** The one cost of revision is flicker (text visibly rewriting). Mitigate by committing the stable prefix of a translation and holding back the volatile tail most likely to change, so revisions rarely rewrite what the viewer already read.

**Important scope note:** the SNMT literature targets unbounded token-by-token streams. This project's source arrives as **discrete, complete OCR'd lines**, so there is no within-line partial problem — only the coarser across-line completion problem. That means revision is rare (only when a new full line completes the previous one) and the simpler subset of the toolkit suffices: translate-with-backward-context, and re-translate-the-pair-with-tail-masking only when N+1 completes N.

Case 2 is **fully solved** only in the pre-processed local-file mode (whole script known before translation). Reactive/streaming mode handles it gracefully but imperfectly; that is the honest accuracy ceiling and it matches what every real-time system and human interpreter achieves.

#### 6b. Reading-time and context passing
- **Sliding context window:** pass the last 2-3 **source** (hanzi) lines with each request as reference context; translate only the current line. Use source not target lines, so a bad earlier translation doesn't poison later context.
- **Display-hold model:** how long a translation stays on screen is decoupled from how long the OCR text was detected. Scale hold by **source (hanzi) character count** (simple, works across all target languages without a per-language table), clamped by a floor (~800ms, ≈ Netflix minimum, so short lines aren't a subliminal flash) and a ceiling (so a garbled/runaway line can't pin the overlay). Subtitle on-screen duration is a hard floor underneath, so the translation never clears while the burned-in hanzi is still visible (they stay aligned). Measure the hold from when the overlay **appears**, so pipeline lag extends the hold rather than eating reading time.
- **Do not linger too long:** a new line clears the previous translation **immediately** on arrival even if its reading-hold hasn't elapsed (sibling of drop-don't-queue: real-time alignment beats reading comfort when they conflict). Keep the ceiling modest.

### 7. Overlay and the latency philosophy
Render the translation pinned to the bottom of the player element (where the hanzi lives), anchored to the player bbox so fullscreen doesn't fling it off.

**The latency promise is a fixed short delay (~0.5-1s) that never grows, not zero lag.** A subtitle is on screen for its full duration; the translation appearing mid-line and riding along for the rest is completely watchable. What is unwatchable is *drift* (falling further behind each line until the overlay refers to a conversation that already ended).

Enforcement: **drop-don't-queue.** If a newer line is already displayed when an older translation returns (checked via `frame_id`), discard the stale result rather than showing it or queuing behind it. On fast dialogue this means occasionally skipping a line, which is invisible; the guarantee is that the translation always belongs to a line currently on screen and the lag never compounds.

A realistic pipeline budget (settle/confirm + OCR + LLM) lands near 500ms even with the fastest model, so chasing sub-500ms is the wrong fight. Designing around a bounded, non-accumulating lag is the correct engineering move.

## Build strategy: reuse vision, write the harness

The vision/OCR stack is all free, local, pip-installable, no API keys:
- **PaddleOCR** (Apache-licensed models), strongest on CJK.
- **videocr-PaddleOCR** / **RapidVideOCR** (Apache 2.0, on PyPI): the threshold/dedup/confidence pipeline with tuned defaults already exposed as parameters.
- **VideoSubFinder** (free, open source): mature background-stripping ("separates text of any color from images"), i.e. our mask step, battle-tested. RapidVideOCR is designed to consume its cleaned-frame output.

These are offline/file-oriented, so we borrow their algorithms and models into a real-time loop (Shape B sidecar) rather than using them as-is. The only paid piece is the optional hosted translation LLM, and even that has a free local fallback.

Strategy: **de-risk with Python (Shape B), ship with wasm (Shape A).** Prove the concept fast against the mature Python stack; port OCR to in-browser wasm for the shippable one-click extension once proven. The contract makes this swap touch only the service side.

## Prior art and honest positioning

This concept is **not novel in the abstract.** "Capture screen pixels, OCR them, overlay a translation, in real time" is a shipping, mature category. Building on the belief that it doesn't exist would be a mistake. But after searching desktop, mobile, browser, and cdrama-specific ecosystems, **the exact intersection — live, hardcoded-pixel OCR, in a browser, cross-platform/Linux, purpose-built for cdramas — is unbuilt.** What follows is the real competitive picture and the honest differentiation.

### The headline differentiation: read the subtitle, not the sound
The primary competitor for cdrama viewers is **Seagull** (getseagull.com): real-time cdrama translation, floating overlay, works over iQIYI/WeTV/Youku/Viki/Netflix, 60+ languages, and **runs on Mac/Windows/Linux.** It is the closest thing to this project's user and platform. **But Seagull is audio-based** — it captures desktop system audio, runs Mandarin speech-to-text, and translates the transcript. It ignores the burned-in subtitle entirely.

This project reads the **burned-in subtitle pixels** instead, and that is a genuine, defensible technical advantage for Chinese specifically:
- **Homophone density is structural, not an ASR bug.** ~400 base syllables (~1300 with tone) map onto tens of thousands of characters. Even with perfect tone detection, one syllable maps to many candidate characters (shī → 师/诗/狮/湿/失/尸/施...). Audio must then guess the character from limited local context. The burned-in subtitle already collapsed that ambiguity — it is the production's own human-disambiguated text.
- **Tone is the first thing to degrade.** ASR must extract an unstable tone contour before it even reaches the homophone guess. Tones flatten in fast speech, shift under sandhi, and native speakers themselves aren't 100% accurate. Two compounding uncertainties the subtitle sidesteps.
- **The acoustic environment is adversarial by genre.** cdramas are whispered intimate scenes, singing (melody overrides linguistic tone by design), shouting/arguing, crowd chants, battle noise, and period/wuxia/xianxia vocabulary the ASR language model never trained on. Every one of these is where audio transcription collapses and the crisp burned-in subtitle is sitting right there.
- **Context resolution comes free.** A human subtitler used *meaning* to pick 诗 over 湿; OCR-ing the subtitle inherits that human contextual judgment. Streaming ASR decodes too locally to match it.

Honest framing (do not overclaim): OCR is not flawless either — its failures are the visual ones in this doc (stylized fonts, white-on-white, borderless-on-busy-background). The real contrast is **OCR's failures are rare and local (a mangled character on a hard frame), audio's failures are frequent and structural (whole lines wrong whenever the scene is loud, sung, fast, or archaic).** Occasional-and-local beats systematic-and-scene-dependent. The one-line pitch: *"Seagull and others translate the audio; this reads the subtitle the show already burned in — the cleaner, human-disambiguated signal, exactly where audio transcription stumbles."*

### Desktop (Windows)
- **Translumo** (open source, .NET 8): explicitly "real-time screen translator for games, **hardcoded subtitles in videos**." Multi-engine OCR with ML scoring to pick the best result (a productized OCR vote), capture-area cropping, DeepL/Google/Papago translation, Chinese supported. Windows-only; recommended engine is WindowsOCR (an OS API); its CJK-capable engines (Tesseract/EasyOCR) are explicitly disrecommended. Translation via scraped web endpoints with user-configured proxy rotation to dodge rate limits.
- **Lexa** (Windows, UWP): "point at the subtitle region of any video player... any media with burned-in captions," smart change detection, in-place overlay. **Gaming/visual-novel focused**, OCR tuned for game UI fonts. **Paid**: free tier caps Realtime at 30 min/day, which does not cover a single 45-minute episode; unlimited needs Pro.

Both are **Windows-only and gaming-first.** Neither runs on Linux (the primary dev/use environment here). Neither is designed around TV-drama viewing.

### Android
The Android screen-translator space is **crowded**, more so than desktop:
- **Instant Translate On Screen**: 5M+ users, 73K reviews, overlays translation on any app/game/manga/video, offline packs, 200+ languages.
- **Screen Translate AI OCR**: has an explicit video-subtitle mode, screen-change auto-capture, and multi-engine OCR including MLKit, Baidu, Zhipu, Tongyi Qwen (CJK-strong). Closest to this design.
- **Screen Translate – Live OCR**, **Video Voice Translate**, **AI Screen Translator** (free/open source), Google Translate's own tap-to-translate bubble, and others.

On Android, "OCR the screen and overlay a translation, incl. video subtitles, Chinese-capable, free" is a **populated category, not a gap.** "Be the first Android screen translator" is not a viable framing.

### The mature "hands-off pipe" (Windows, gaming, gated)
The continuous, hands-off (not tap-to-translate) pixel pipe **is** solved, but only on Windows for gamers:
- **Game-Changing Translator (GCT, tomkam1702):** continuous, context-aware (sliding window of 5 prior subtitles), AI OCR (Gemini/Gemma) for stylized/low-contrast text, in-place overlay, dedup caching. Feature-for-feature the closest to this design. **But:** Windows-only; game-tuned (every demo is an RPG); the cdrama-critical features are **PRO-gated** (DeepL Chinese translation, "Find Subtitles" region-lock, "Target on Source" in-place overlay); v4 is API-key-required; and it is **proprietary (EULA)** — studyable for ideas, not forkable.

The three mature tools (Lexa, Translumo, GCT) **converged on the same three choices**: Windows, gaming-first, paywalled (at least for CJK). That convergence is not coincidence — they all chased the paying Windows PC gamer and left the same corner unbuilt: free, cross-platform, cdrama-first.

### The browser lane (scattered pieces, no assembled whole)
In-browser, the components exist but nobody has assembled the full row:
- **yt-sub-ocr** (userscript): crops a YouTube video region and OCRs hardcoded subs with **paddle-wasm** (proves the OCR engine works in-browser on the exact input) — but YouTube-only, **pauses the video**, no translation, no overlay, manual. Forkable reference for paddle-wasm-on-a-crop.
- **Crivella/ocr_extension** (Firefox): extension → self-hosted OCR+translate backend → in-place overlay, **tested on Linux** (proves the extension↔sidecar↔overlay-on-Linux architecture) — but for static page images, not video. Forkable reference for the Shape-B wiring.
- **Copyfish** (mature, Chrome/FF): names the exact use case ("movie subtitles on YouTube or Youku"), "mark the subtitle area once, then Do OCR" — but manual tap-repeat, not continuous.
- **Audio-based browser tools** (Immersive Translate, TranslateSub): continuous and hands-off, but read caption tracks / audio, not pixels.

The consistently missing piece across all of them is the same one: **continuous, hands-off, pixel-OCR of a subtitle region, in a browser.** That specific cell is empty.

### Cdrama-specific tools (audio-live or offline-batch, never live-pixel-browser)
- **Live + audio:** Seagull (above) — the primary competitor; audio, not pixels.
- **Offline + pixel-OCR:** GeekLink, Mediaio, the C-Drama Subtitles / Stardust TV (Gemini-on-Vertex) pipelines — these OCR hardcoded cdrama subs and translate to many languages, but they are **batch, file-based, production/distributor tools** (make an SRT / burn a new sub for upload), not live viewer tools.

Market signal from that ecosystem: Chinese short dramas did ~¥50B ($7B) in 2025, overseas growing 300%+ YoY, Thailand the #1 foreign market, "demand for localization far outstrips supply," "most of the best ones don't come with English subtitles." The underserved-audience thesis is confirmed with revenue.

### The genuine differentiation
Every general tool above is aimed primarily at manga, games, and social media, with video/subtitles as one mode among many. None is a purpose-built **cdrama viewing experience.** The differentiation is method (pixel vs audio, above) plus positioning and polish:
1. **Cross-platform / Linux-first.** Nothing above runs as a browser tool on Linux. The browser extension on Linux is genuinely uncontested.
2. **Subtitle-band-locked**, with watermark/prop rejection (the detector model in sections 3a-3c), vs. general tap-to-translate or whole-screen OCR that also grabs logos, UI, timestamps.
3. **LLM translation with dialogue context** (tone, pronouns, continuity across a conversation) vs. generic per-fragment MT. Matters most for dialogue-dense narrative drama.
4. **Broadcast-hanzi-subtitle-tuned** OCR, vs. game-UI-font or manga tuning.
5. **Lean-back continuous-viewing UX**, vs. tap-and-read rhythms built for manga/games.

### Sober conclusion (read this before over-investing)
- As a **product to win a market**: the positioning wedge (cross-platform + cdrama-first + browser-native) is real but modest; general tools are free and entrenched. **But the pixel-vs-audio method advantage is the one durable, non-positioning edge** — it's a technical quality difference, not just packaging, and it holds specifically against Seagull, the one competitor sharing this exact user and platform. That's the argument worth leading with; it survives someone finding Seagull.
- As a **thing to build and use**: fully justified. The stated goal is "translated cdramas in my life (on Linux) + the satisfaction of building it." No existing tool serves that: Lexa/Translumo/GCT are Windows-only and gaming/paywall-gated, the offline cdrama tools are batch/production, Seagull is audio (and its errors are exactly the ones this method avoids), and **nothing is a live-pixel browser extension on Linux**, the actual daily-driver environment and first target. The lane is uncontested; the motivation survives every competitive fact above intact.

**Build the browser extension** because it doesn't exist for this platform and it solves the real itch. Treat Android as someday-maybe, entered knowingly as a specialist among generalists, not as a first mover. Let the browser build reveal whether the cdrama-specific polish is a difference users actually feel before committing to mobile.

## Platform roadmap
Shared across platforms: the **pipeline logic and the contract** (mask, OCR vote, dedup, drop-don't-queue, bounded lag, keep-source-text). Platform-specific: capture, OCR, and overlay implementations.
- **Browser (now):** Firefox extension. Capture via `getDisplayMedia` / `<video>` canvas; OCR via Python sidecar (Shape B) then wasm (Shape A); overlay via a positioned DOM element on the player.
- **Android (someday-maybe):** Native app. Capture via **MediaProjection** (OS-level, works over any app incl. Youku/bilibili/WeTV); OCR via **Google ML Kit** on-device (free, no keys, CJK-capable) or PaddleOCR-mobile; overlay via a system "display over other apps" floating window. Note Android 14+ requires `foregroundServiceType="mediaProjection"` and re-granting capture permission per session. The incumbents (Windows desktop) structurally cannot follow here, and cdrama viewers skew mobile.
- **iOS (never):** The sandbox forbids a persistent app-drawn overlay over arbitrary other apps, which is the core mechanic. This is why no such tool exists on iOS. Also not a personal-use platform here.

## Distribution note
Primary: addons.mozilla.org. The one UX tax is the `getDisplayMedia` share prompt and banner; get ahead of it in the listing ("you'll be asked to share the tab; this is how it reads subtitles; nothing leaves your machine but the text being translated"). Lead with the honest differentiators: cross-platform/Linux, cdrama-purpose-built, subtitle-locked, LLM-context translation, any target language. Do not claim first-to-concept; claim first cdrama-focused browser-native tool.