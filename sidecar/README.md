# sidecar — local FastAPI service (Shape B)

The local companion process that owns OCR, the 3-frame majority vote, dedup, and
translation. It runs on `127.0.0.1` and answers the `POST /translate` contract
from `CLAUDE.md`. The extension never learns what's behind the endpoint — that's
the whole point of the seam, and it's how Shape B (this Python process) later
becomes Shape A (in-browser wasm) without the extension changing.

**This is a local process, not a hosted/cloud server.** Nothing you deploy,
nothing with users. In session 2 it does *not* even translate for real
(translation is mocked) and makes no network calls at all.

## Status: fully real pipeline (RapidOCR + Groq)

The endpoint, the Pydantic contract, the per-character majority vote, dedup,
confidence/consensus gating, CORS, and the swappable seams are all in and tested
end-to-end, and the extension calls this service for real. **OCR and translation
are both real now**; measured ~390-500ms full round-trips (3 real OCR frames +
vote + Groq), at the documented budget.

**OCR is real.** `RapidOcrEngine` (ocr.py) runs RapidOCR (ONNX Runtime + PP-OCR
models, strong on CJK — and the ONNX/PP-OCR pipeline the wasm Shape A will
mirror). `make_ocr()` uses it when importable, else the mock; `CDT_OCR=mock`
forces the mock. Installs clean on Python 3.14 (no paddlepaddle).

**Mask-before-OCR (§4) is on** (`mask_for_ocr`, `OCR_MASK=True`): the raw crop is
background-subtracted (near-white fill next to a dark stroke kept, rest → black)
before recognition, so moving clutter can't be read as a stray glyph. On
adversarial synthetic clutter this took exact reads from 2/6 → 6/6 with **no**
latency cost (~40ms/frame). Two landmines learned and encoded: a black *border*
on the masked image makes DBNet detection explode (~40ms → ~1200ms, so
`OCR_MASK_PAD=0`), and the confidence gate had to drop to 0.50 because short CJK
lines score low and masking made the gate — not clutter — the thing dropping
correct reads (走吧 @ 0.54).

**Adaptive fallback** (`OCR_MASK_ADAPTIVE=True`, `OCR_MASK_FALLBACK_CONF=0.65`)
handles the other side of that trade: masking helps against clutter but can hurt
thin-stroke / low-contrast text. So when the masked read comes back weak (below the
fallback conf), the engine also OCRs the **raw** crop and keeps whichever the
recognizer trusted more. The second pass runs only on weak frames, so the common
case still costs one OCR. It's entirely behind the OCR seam — the extension never
knows, and Shape A can mirror or drop it freely. `OCR_MASK=False` still skips
masking altogether (raw only).

**Translation is real now.** `GroqTranslator` (translate.py) calls Groq's
chat-completions API. `make_translator()` picks Groq when `GROQ_API_KEY` is set
(loaded from the repo-root `.env`), else the mock; `CDT_TRANSLATOR=mock` forces
the mock for offline work. Model is `GROQ_MODEL` (default `qwen/qwen3.6-27b` —
Qwen is trained heavily on Chinese and edges out llama on nuance/idiom). Qwen is
a *thinking* model, so the client auto-sends `reasoning_effort: "none"` to
suppress `<think>` traces (overridable via `GROQ_REASONING_EFFORT`) and strips
any stray trace defensively. Caveat: `qwen/qwen3.6-27b` is a Groq **preview**
model — fine for personal use, but Groq may change/pull it; fall back with
`GROQ_MODEL=llama-3.3-70b-versatile`. ~120-430ms round-trips.
The system prompt treats OCR as a noisy channel — tolerate character glitches,
don't invent — and preserves dramatic nuance (idiom/tone, no flattening).

**Split-sentence re-translation (6a) is on, with a two-layer speaker guard.** The
extension flags a *candidate* continuation from cheap signals — previous line had
no terminal punctuation **and no sentence-final particle** (吗/吧/呢/嘛, so a
question like 你爱我吗 doesn't bridge onto its answer) and the gap was short — then
holds/​revises the overlay in place. The service is the arbiter: it **defaults to
separate** (translate only the last line) and fuses into one sentence only on an
unmistakable mid-clause split (虽然…/如果…). So 虽然他非常努力 / 但最后还是失败了 →
*"Although he worked very hard, he still failed in the end,"* but 你到底爱不爱我 /
我爱你才怪 stays as just the reply.

Honest ceiling: without speaker labels, a genuinely ambiguous statement+objection
(这件事就这么定了 / 我不同意) will *occasionally* still fuse — inherent to text-only
speaker separation. The particle layer is deterministic; the arbiter is
best-effort; together they strictly beat the old fuse-whenever-punctuation-absent.
Context window stays at 2-3 lines on purpose: bigger doesn't help single-line
translation and pulls in more cross-speaker leak.

Deferred refinements (6a): **tail-masking** (commit the stable prefix so a
revision rarely rewrites read text) and **OCR-uncertainty passing** (hand the
model the per-position vote disagreements so it repairs look-alike substitutions
with information rather than guessing — see the caveat in the top-level README).

### Translation config (.env at repo root)

```
GROQ_API_KEY=gsk_...            # required for real translation; gitignored
GROQ_MODEL=qwen/qwen3.6-27b          # optional override (default; qwen is best on CN)
GROQ_REASONING_EFFORT=none           # optional; auto-set for qwen/qwq already
CDT_TRANSLATOR=mock             # optional: force the mock translator even with a key
CDT_OCR=mock                    # optional: force the mock OCR (offline / contract tests)
```

## Auditing translations

Each sidecar run writes its own `logs/cdrama-<YYYYMMDD-HHMMSS>-<model>.jsonl`
(gitignored), one JSON line per request — so sessions (and models) never mix,
which makes A/B'ing llama vs qwen on the same footage a matter of two runs. Every line captures the
**whole pipeline** so a bad output can be traced to the stage it went wrong — not
just the final translation:

- `stage:"translate"` (sidecar): `video_time` (jump back to the exact frame),
  per-frame `reads` (raw hanzi + conf, **pre-vote**), voted `source_text` + mean
  `confidence`, `context_lines`, `continuation` (the ↳cont candidate flag),
  `translation`, `latency_ms`, `provider`/`model`, `status`, `label` (episode).
- `stage:"display"` (extension, reported back via `POST /log`): `visible_ms` and
  `outcome` (`expired` / `preempted` / `dropped` / `revised`), joined to the
  translate record by `frame_id` — this is how you catch a good translation that
  was shown too briefly or never shown.

`CDT_LOG=0` disables logging; `CDT_LOG_DIR=...` relocates the folder.

Analyse with `audit.py` — it defaults to the **most recent run**:

```
python audit.py                  # quick stats: status, confidence, latency, display outcomes
python audit.py --all            # combine every run in the log dir
python audit.py --pipeline 20    # last 20 lines' full per-stage trace (reads -> voted -> ctx -> translation -> display)
python audit.py --metrics        # the accuracy STACK (OCR / consistency / latency), no LLM
python audit.py --judge 40       # GEMBA-MQM: adequacy given the source (LLM, offline)
python audit.py logs/cdrama-20260718-*.jsonl --label 四合院 --lang en
```

### Measuring accuracy: a stack, offline — not one live score

For a live-OCR pipeline "accuracy" isn't a single number, and it can't be measured
in the hot path. We have **no reference translations**, so only reference-free
methods apply; the OCR'd source can be wrong (a faithful translation of a garbled
line is still wrong — no MT metric catches that); the sliding context window makes
term scatter *structural*; and a live system trades quality against latency. So the
audit measures the **stages the log already separates**, all after the fact:

- **`--metrics` (cheap, no LLM):** the whole stack from the log alone —
  - *OCR fidelity:* confidence distribution, `no_text`/`low_confidence` share, and
    how often the 3 frames disagreed (the vote had to arbitrate) — an OCR-uncertainty
    proxy, because translation quality is capped by what OCR read.
  - *terminology consistency:* identical-line stability (same source → same English?)
    and glossary adherence (does a recurring pinned term keep its rendering?) — the
    scatter the glossary targets, now a first-class metric.
  - *latency & live health:* latency percentiles vs. the 600ms budget, display
    outcomes, `dropped` (translated but never shown = wasted/late), and lines shown
    too briefly to read. Quality × latency is the real axis for live MT.
- **`--judge` (LLM, offline):** translation adequacy/fluency *given the source*, as
  **GEMBA-MQM** — the model annotates errors by category (mistranslation, omission,
  addition, terminology, grammar, punctuation, register) × severity (minor 1 / major
  5 / critical 10), and we **aggregate several independent votes** per line
  (`GROQ_JUDGE_VOTES`, default 3) keeping only majority-confirmed errors. Output is a
  clean-rate, per-category incidence, and the worst lines with jump-to timestamps.

The judge runs **separately on a jsonl, never live** — it's several LLM calls per
line and would blow the bounded-lag budget on the wire. Because it's a *batch* pass,
quality beats speed, so the grader is decoupled from the (Groq) translator:
`JUDGE_PROVIDER` (`groq` | `openrouter`) + `JUDGE_MODEL` pick it. **Prefer a grader
with strong Chinese** — MQM adequacy means reading the source, and llama is a fine
*fluency* judge but weak on hanzi; via OpenRouter (`OPENROUTER_API_KEY`) use
`deepseek/deepseek-chat`, a Claude, or a GPT-class model, which read the source
properly and aren't the same family as the Qwen translator (no self-eval / correlated
blind spots — the run prints a `SELF-EVAL` warning if grader == translator). Votes
auto-use a small temperature when >1 so they actually vary (temp 0 would make "N
independent votes" identical). Better still, set `JUDGE_MODELS` to a comma-separated
**panel** (e.g. `deepseek/deepseek-chat,google/gemini-2.5-flash,anthropic/claude-…`):
one vote per model, so independence comes from *diverse families* — which is the
documented way to bound judge bias (self-preference runs 10–25% within a family). The
judge deliberately ignores OCR garble in the *source* (that's the OCR stage's job,
covered by `--metrics`), so the two passes don't double-count. Every run prints a
**cost line** (real token counts; OpenRouter's billed cost when available, else a
rough estimate) — a 40-line × 3-vote audit is ~2¢ on DeepSeek, so treat it as free.
All three judge tools route through `judge_llm.py`, so switching the grader (or panel)
is one env change and never touches translation.

### Drift judge — `drift_judge.py` (cross-line consistency only)

A separate, *windowed* judge for the one thing per-line grading can't see: errors
that arise from the **relationship between lines**. It shows a non-Qwen grader a
window i-2…i+1 with the target marked and asks only about cross-speaker leak,
stale-referent, register drift, tense/aspect flip, and pronoun-gender consistency.
Leak/referent findings must name a `source_scope` — `immediate-neighbor` (the
continuation arbiter fused adjacent lines) vs `scene-level` (the translator lost
track of scene state). That field is the decision signal for whether a scene-level
context intervention is warranted at all.

```
python drift_judge.py [log]              # RANDOM sample -> the population gate rate
python drift_judge.py [log] --stratified # oversample risky windows -> SHAPE, not the gate
```

Two samples, two jobs: **random** gives the gate rate the roadmap keys on;
**--stratified** (continuation / pronoun-context windows) characterizes shape and
severity but over-estimates frequency, so it's never the gate. The grader defaults
to `llama-3.3-70b-versatile` (not Qwen — avoid self-eval) and aggregates
`GROQ_DRIFT_VOTES` votes (default 3), keeping only majority-confirmed findings.
**Judge output is candidates, not truth** — it over-flags the same way a regex
would, so confirmed instances are written to `<log>.drift-<mode>.verify.jsonl` with a
`"verified": null` slot to fill in by hand before trusting the rate.

### Persona A/B harness — `persona_ab.py` (built; finalize the arms, then run)

Tests whether a persona reframe and/or two explicit clauses (audience + commit) beat
the current prompt, as a clean 2×2 (framing × clauses) so persona and clauses are
isolated. `control` is the production prompt verbatim; the shared rule block is
*derived* from it (strip the framing sentence), so each arm differs only by the factor
under test. The system prompt is overridden by seeding the translator's prompt cache —
no production code is touched.

```
python persona_ab.py --dry-run    # sample + assemble arms, NO api calls (inspect first)
python persona_ab.py [log] --guardrail 400 --targeted 80 --votes 3   # the real run
```

Two strata: a **random guardrail** (regression check — did an arm make things worse?)
and a **blind targeted** stratum of convention-bearing lines (name-tag / honorific /
chengyu) selected by *source pattern only*, never by whether the current prompt gets
them right, with the matching rule recorded. Judged paired with GEMBA-MQM; the report
gives per-arm clean-rate + McNemar discordant pairs vs control (flagged underpowered
below 30). **The `_FRAME_SUB` and `_CLAUSES` drafts are placeholders — finalize them
before a real run;** `--dry-run` prints all four assembled prompts to inspect the
control-vs-arm diffs.

### Corpus gate (both measurement tools)

A number from one episode of one show fits *that episode*, not the phenomenon —
drift and prompt effects can be genre-specific. So both `drift_judge.py` and
`persona_ab.py` enforce a corpus gate in code:

- **Multi-log input** — pass several logs or `--all` to measure across a corpus;
  `--holdout SUBSTR` reserves a validation episode (never used for the pass) so you
  can check whether a tuning tweak generalizes.
- **Per-show before aggregate** — results break out per show (grouped by a `《…》`/
  title heuristic); the aggregate never stands alone.
- **Divergence → DEFER** — the drift judge defers if per-show *rates* spread >2×; the
  A/B defers if an arm's *effect* flips sign across shows (raw per-show clean-rate
  spread is expected and is not the trigger). A show needs ≥8 judged items to weigh in.
- **Below-gate banner** — under 3 substantial shows (≥30 lines each; stray-tab lines
  are flagged incidental), every run is labelled **WIRING VALIDATION ONLY, not
  evidence**. The current single-episode log trips this by design.

Minimum before any run counts as evidence: **≥3 shows across ≥2 genres, ≥5 episodes,
and ≥1 held-out episode.**

## Re-watch track — `track.py` (offline high-quality, the other corner of the frontier)

Live translation is bounded-lag / mostly-right and sees only *past* context. `track.py`
is the opposite corner: an offline pass where a strong **teacher** model re-translates
every logged line with a window of surrounding lines — past *and future* — the context
the live model never had. It follows the same house rules as the live prompt (name-tag
elision, interrogative-without-吗, no leak, register), just with better context and a
stronger model, so the track matches the live style but reads better.

```
python track.py [log] --out mytrack.json --window 8
env: TEACHER_MODEL (else JUDGE_MODEL), JUDGE_PROVIDER, OPENROUTER_API_KEY / GROQ_API_KEY
```

Output is the tool's **own** `frame_id`-keyed replay format (per CLAUDE.md invariant 1
— a **local, personal** re-watch artifact, deliberately *not* a portable `.srt`, never
shared/uploaded). Each cue carries `frame_id` (the pixel-line id), `t` (video seconds —
when to show), `dur`, the `source` hanzi, the corrected `text`, and the original `live`
line for comparison. The extension's replay mode plays these against the video's
`currentTime`. Reuses `judge_llm`'s provider routing + cost tally, and doubles as the
distillation teacher pass (full-context targets for a streaming student). *(Glossary /
context-note injection into the teacher is a noted follow-up.)*

**Capturing a clean log** (a track is only as good as its log): capture in a **single
forward pass** — turn on **capture mode** in the panel (it pauses per line so a slow
pipeline never misses one, and pauses give clean, motion-blur-free OCR) and let it run
start-to-end. Capture mode is *pixel-driven*, not timestamp-driven — it reacts to what's
on screen and records `video_time`, so a monotonic pass yields a monotonic track. If you
seek/rewind mid-capture it's handled, not fatal: the extension detects the jump and resets
continuity across the cut (no continuation bridge, stale context, or dedup leak), and
`track.py` drops re-visited lines — but a single pass is cleanest. Replay, by contrast, is
timestamp-driven (`currentTime` lookup) and fully seek-safe on playback.

## Consistency glossary

Each line is translated independently, so a recurring term (黄羊, a character
name) scatters across renderings. The glossary pins each recurring term to one
translation and injects only the terms present in the current line (0-2, so the
prompt never bloats). Two layers, both plain JSON, human- and LLM-editable:

- `glossary/universal.json` — curated, shareable, **genre-neutral terms only**
  (黄羊 → Mongolian gazelle, 原子弹 → atomic bomb). Never character names — those
  collide across shows. Fixes universal-term *correctness* from the first line.
- `glossary/shows/<slug>.json` — per-show, **auto-built** from a run's log by an
  LLM extraction pass; names/places/show-specific terms (gitignored, personal).

Curated universal wins conflicts (so a hand-corrected term beats noisy
auto-extraction). Build a per-show glossary from a run:

```
python glossary.py build logs/cdrama-<...>.jsonl
```

~34 distinct terms per episode; converges fast. Consistency, not correctness —
auto-built entries pin whatever the model first read, so edit the JSON (or the
seed) to fix a rendering. Empty/delete the files to disable; `CDT_GLOSSARY_DIR`
relocates them. Which terms were pinned per line is in the audit log's
`glossary` field.

```
sidecar/
  app.py            FastAPI app + POST /translate orchestration (the pipeline)
  contract.py       TranslateRequest / TranslateResponse — the contract as Pydantic
  ocr.py            OcrEngine seam + MockOcr (PaddleOcr drops in here)
  vote.py           per-character majority vote across the 3 frames + consensus
  dedup.py          string-similarity dedup vs last_shipped_text
  translate.py      Translator seam + MockTranslator (Groq/Ollama drop in here)
  config.py         thresholds (videocr-derived defaults)
  test_client.py    standalone HTTP exercise of every status path
  requirements.txt  fastapi, uvicorn, pydantic
  run.sh            launch helper
```

## Run it

```bash
cd sidecar
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh                       # or: .venv/bin/python -m uvicorn app:app --port 8000 --reload
```

- Interactive contract docs + a click-to-test UI: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

## Test it (standalone, no browser)

With the server running, in another terminal:

```bash
cd sidecar && .venv/bin/python test_client.py
```

It drives every path: a normal line (the vote corrects a planted single-frame
misread 要→娶), a duplicate (dedup, no re-translation), a silent box
(`no_text`), and two contract-enforcement cases (too many frames, unknown field
→ `422`). Expect `14 passed, 0 failed`.

## Contract (authoritative copy in `CLAUDE.md`)

```
POST /translate
{ "frames": ["<b64 png>", ...1-3], "source_lang": "ch", "target_lang": "en",
  "frame_id": 4821, "last_shipped_text": "<what the extension last displayed>",
  "context_lines": ["<prev source line>", ...last 2-3 hanzi lines],
  "continuation": false, "context_note": "<optional show/episode background>",
  "tone": "<optional register lean: casual|formal|literary|playful|romantic|business>" }

-> { "frame_id": 4821, "status": "ok|no_text|duplicate|low_confidence",
     "source_text": "<hanzi>", "translation": "<target>", "confidence": 0.94,
     "duplicate": false }
```

`extra="forbid"` on the request means an unrecognized field is a loud `422`, not
a silent drift — the extension and service can't fall out of contract unnoticed.

## Next step — real OCR, and a Python-version caveat

PaddleOCR + paddlepaddle is a heavy install (hundreds of MB of wheels + model
downloads). Note this repo's default `python3` is **3.14**, and paddle wheels
historically lag new Python releases — if `pip install paddlepaddle` finds no
3.14 wheel, we'll stand the sidecar's venv up on Python 3.11/3.12 instead (it's
behind the contract, so its runtime is a free choice). RapidOCR/ONNX is the
fallback OCR if paddle is uncooperative. The mock lets everything downstream —
including session 3's extension wiring — proceed regardless.
# cdrama-translator
